"""Authentication precedes reads, and a fake TLS server never receives credentials."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import mcp_types
import pytest
from mcp.client import Client
from mcp.server import Server
from narumi_server.secure_transport import load_client_transport, prepare_server_transport
from narumi_server.stdio_bridge import authenticated_streams
from narumi_server.transport_auth import LocalAuthenticationMiddleware
from narumi_server.transport_logging import TransportLogFilter, install_transport_log_filters
from secure_transport_helpers import MemorySecretStore, free_port, running_server, serve_tls
from starlette.responses import JSONResponse, RedirectResponse

FAKE_KEY = "fake-transport-key-874352"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_session_uses_authenticated_tls(
    home: Path, caplog: pytest.LogCaptureFixture, mode: Any
):
    seen: list[dict[str, Any]] = []

    async def list_tools(_ctx: Any, _params: Any):
        return mcp_types.ListToolsResult(
            tools=[mcp_types.Tool(name="probe", input_schema={"type": "object"})]
        )

    async def call_tool(_ctx: Any, params: Any):
        seen.append(params.arguments)
        return mcp_types.CallToolResult(content=[], structured_content={"ok": True})

    caplog.set_level(logging.DEBUG)
    server = Server("secure-test", on_list_tools=list_tools, on_call_tool=call_tool)
    async with running_server(server, home) as (_, connection):
        async with Client(authenticated_streams(connection), mode=mode) as client:
            assert [tool.name for tool in (await client.list_tools()).tools] == ["probe"]
            result = await client.call_tool("probe", {"api_key": FAKE_KEY})
            assert result.structured_content == {"ok": True}
        assert connection.client_token not in caplog.text
    assert seen == [{"api_key": FAKE_KEY}]
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize(
    "override",
    [
        [(b"authorization", b"Bearer wrong")],
        [(b"origin", b"https://attacker.example")],
        [(b"host", b"localhost:8765")],
        [(b"forwarded", b"host=attacker.example")],
        [(b"x-forwarded-proto", b"https")],
    ],
)
async def test_every_method_rejects_untrusted_headers_without_reading_body(method: str, override):
    called: list[bool] = []

    async def endpoint(*_args: Any):
        called.append(True)

    async def receive():
        pytest.fail("the body must not be read before authentication")

    headers = [(b"host", b"127.0.0.1:8765"), (b"authorization", b"Bearer fixture")]
    names = {name for name, _ in override}
    headers = [(name, value) for name, value in headers if name not in names] + override
    scope = {
        "type": "http",
        "scheme": "https",
        "path": "/mcp",
        "method": method,
        "headers": headers,
    }
    messages: list[dict[str, Any]] = []

    async def send(message):
        messages.append(message)

    await LocalAuthenticationMiddleware(
        endpoint, url="https://127.0.0.1:8765/mcp", client_token="fixture"
    )(scope, receive, send)
    assert messages[0]["status"] == 401
    assert not called


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_duplicate_authorization_and_plaintext_never_reach_mcp(scheme: str):
    async def forbidden(*_args):
        pytest.fail("an untrusted request reached MCP")

    responses = []

    async def send(message):
        responses.append(message)

    await LocalAuthenticationMiddleware(
        forbidden, url="https://127.0.0.1:8765/mcp", client_token="fixture"
    )(
        {
            "type": "http",
            "scheme": scheme,
            "path": "/mcp",
            "method": "POST",
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"authorization", b"Bearer fixture"),
                (b"authorization", b"Bearer fixture"),
            ],
        },
        forbidden,
        send,
    )
    assert responses[0]["status"] == 401


async def test_fake_certificate_and_plain_http_receive_no_request(home: Path):
    store = MemorySecretStore()
    requests: list[Any] = []
    port = free_port()

    async def fake_app(scope, receive, send):
        if scope["type"] == "http":
            requests.append(scope)
            await JSONResponse({"ok": True})(scope, receive, send)

    with (
        prepare_server_transport(
            home / "real", str(uuid4()), port=port, secret_store=store
        ) as real,
        prepare_server_transport(
            home / "fake", str(uuid4()), port=port, secret_store=store
        ) as fake,
    ):
        connection = load_client_transport(home / "real", secret_store=store)
        async with serve_tls(fake_app, fake):
            async with httpx2.AsyncClient(
                verify=connection.ssl_context,
                trust_env=False,
                timeout=1,
                headers={"Authorization": f"Bearer {connection.client_token}"},
            ) as client:
                with pytest.raises(httpx2.ConnectError):
                    await client.post(real.url, json={"api_key": FAKE_KEY})
                with pytest.raises(httpx2.RequestError):
                    await client.post(
                        real.url.replace("https:", "http:"), json={"api_key": FAKE_KEY}
                    )
    assert requests == []


async def test_invalid_rpc_never_echoes_secret(home: Path, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    async with running_server(Server("parse-test"), home) as (_, connection):
        async with httpx2.AsyncClient(
            verify=connection.ssl_context,
            trust_env=False,
            headers={"Authorization": f"Bearer {connection.client_token}"},
        ) as client:
            response = await client.post(
                connection.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": FAKE_KEY},
            )
            assert response.status_code == 400
            assert FAKE_KEY not in response.text
    assert FAKE_KEY not in caplog.text


def test_dependency_log_filter_removes_exception_and_argument_values(caplog):
    caplog.set_level(logging.DEBUG)
    install_transport_log_filters()
    log = logging.getLogger("mcp.client.streamable_http")
    try:
        raise ValueError(FAKE_KEY)
    except ValueError:
        log.exception("payload: %s", {"api_key": FAKE_KEY})
    record = caplog.records[-1]
    assert FAKE_KEY not in caplog.text
    assert record.args == () and record.exc_info is None and record.exc_text is None
    assert TransportLogFilter().filter(record)


async def test_bridge_http_client_refuses_redirect_and_environment_proxy(home: Path, monkeypatch):
    calls = []
    store = MemorySecretStore()

    async def redirect(scope, receive, send):
        if scope["type"] == "http":
            calls.append(scope["path"])
            await RedirectResponse("https://127.0.0.1:1/stolen", status_code=307)(
                scope, receive, send
            )

    with prepare_server_transport(
        home, str(uuid4()), port=free_port(), secret_store=store
    ) as credentials:
        connection = load_client_transport(home, secret_store=store)
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        async with serve_tls(redirect, credentials):
            with pytest.raises(ExceptionGroup):
                async with Client(authenticated_streams(connection)):
                    pytest.fail("a redirect must not produce a session")
    assert calls == ["/mcp"]
