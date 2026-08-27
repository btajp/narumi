"""Gaia contract-v1 client tests using an in-process HTTP fake, never a real Gaia service.

The fake is also reused by exporter tests. All fixtures use the actual contract structures,
including get_server_info, so a draft-schema response cannot accidentally pass as empty data.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from narumi.errors import ContractMismatchError, EngineUnavailableError, ErrorCode, NarumiError
from narumi.gaia import ENV_GAIA_API_KEY, ENV_GAIA_URL, GaiaClient

SESSION_ID = "sess-1234"
CORE_TOOLS = [
    "get_server_info",
    "search_context",
    "get_engagement",
    "get_glossary",
    "resolve_speakers",
    "propose_update",
]


def tool_ok(structured: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
        "structuredContent": structured,
        "isError": False,
    }


def tool_error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"error": {"code": code, "message": message, "details": details}},
        "isError": True,
    }


def server_info() -> dict[str, Any]:
    return {
        "name": "gaia_library",
        "version": "0.1.0",
        "contract_version": "1.0.0",
        "protocol": {"transports": ["stdio", "http"]},
        "capabilities": {"tools": list(CORE_TOOLS), "resolvers": [], "search": {"fts": "trigram"}},
        "client": {"name": "narumi", "role": "agent", "default_scope": "cn"},
    }


def empty_search(query: str = "") -> dict[str, Any]:
    return {
        "query": query,
        "scopes": ["cn"],
        "cross_scope": False,
        "entities": [],
        "glossary": [],
        "interactions": [],
        "hints": [],
    }


def engagement_result(name: str = "acme", scope: str = "cn") -> dict[str, Any]:
    return {
        "engagement": {"id": 42, "name": name, "scope": scope},
        "people": [],
        "facts": [],
        "refs": [],
        "glossary": [],
        "interactions": [],
    }


class FakeGaiaServer:
    """Bounded-lifetime local endpoint; tools maps names to functions returning MCP results."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.info = server_info()
        self.tools: dict[str, Any] = {"get_server_info": lambda _: tool_ok(self.info)}
        self.rpc_errors: dict[str, dict[str, Any]] = {}
        self.session_id = SESSION_ID
        self.sse = False
        self.fail_next_call_with_404 = False
        self.http_status: int | None = None
        self.http_body = b""
        self.redirect_url: str | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                server.frames.append({"method": "HTTP_GET"})
                self._http_error(405, b"")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                server.frames.append(body)
                server.headers.append(dict(self.headers.items()))
                if server.http_status is not None:
                    self._http_error(server.http_status, server.http_body)
                    return
                method = body.get("method")
                if "id" not in body:
                    self._http_error(202, b"")
                    return
                if method == "initialize":
                    self._reply(
                        body["id"],
                        {
                            "result": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "gaia_library", "version": "0.1.0"},
                            }
                        },
                        session=server.session_id,
                    )
                    return
                if method == "tools/call":
                    if server.fail_next_call_with_404:
                        server.fail_next_call_with_404 = False
                        self._http_error(404, b"session not found")
                        return
                    name = body["params"]["name"]
                    if name in server.rpc_errors:
                        self._reply(body["id"], {"error": server.rpc_errors[name]})
                        return
                    fn = server.tools.get(name)
                    if fn is None:
                        self._reply(
                            body["id"],
                            {
                                "error": {
                                    "code": -32602,
                                    "message": f"unknown tool `{name}`",
                                    "data": {
                                        "code": "not_found",
                                        "message": f"unknown tool `{name}`",
                                        "details": {"tool": name},
                                    },
                                }
                            },
                        )
                        return
                    self._reply(body["id"], {"result": fn(body["params"].get("arguments") or {})})
                    return
                self._reply(
                    body["id"],
                    {"error": {"code": -32601, "message": f"Method not found: {method}"}},
                )

            def _http_error(self, code: int, raw: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                if server.redirect_url is not None:
                    self.send_header("Location", server.redirect_url)
                self.end_headers()
                self.wfile.write(raw)

            def _reply(
                self, rpc_id: Any, payload: dict[str, Any], session: str | None = None
            ) -> None:
                message = {"jsonrpc": "2.0", "id": rpc_id, **payload}
                if server.sse:
                    raw = ("event: message\ndata: " + json.dumps(message) + "\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    raw = json.dumps(message).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=lambda: self._httpd.serve_forever(poll_interval=0.01), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def call_frames(self) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame.get("method") == "tools/call"]


@pytest.fixture()
def gaia_server():
    server = FakeGaiaServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_initialize_handshake_contract_gate_and_session_header(gaia_server: FakeGaiaServer):
    gaia_server.tools["search_context"] = lambda args: tool_ok(empty_search(args["query"]))
    client = GaiaClient(gaia_server.url)
    assert client.search_context("定例", scope="cn") == empty_search("定例")
    assert [frame.get("method") for frame in gaia_server.frames] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
    ]
    init = gaia_server.frames[0]
    assert init["params"]["protocolVersion"] == "2025-06-18"
    assert init["params"]["clientInfo"]["name"] == "narumi-pipeline"
    assert "id" not in gaia_server.frames[1]
    assert gaia_server.frames[2]["params"] == {"name": "get_server_info", "arguments": {}}
    assert gaia_server.frames[3]["params"] == {
        "name": "search_context",
        "arguments": {"query": "定例", "scope": "cn"},
    }
    assert all(headers.get("Mcp-Session-Id") == SESSION_ID for headers in gaia_server.headers[1:])
    client.search_context("次回")
    assert len(gaia_server.frames) == 5
    assert gaia_server.headers[4]["Mcp-Session-Id"] == SESSION_ID


def test_sse_and_scoped_engagement_name_resolution(gaia_server: FakeGaiaServer):
    gaia_server.sse = True
    gaia_server.tools["get_engagement"] = lambda args: tool_ok(engagement_result(args["name"]))
    glossary = {
        "terms": [{"id": 3, "term": "SCIM", "reading": "スキム", "scope": "cn"}],
        "vocabulary_hints": ["SCIM", "スキム", "田中"],
    }
    gaia_server.tools["get_glossary"] = lambda _: tool_ok(glossary)
    assert GaiaClient(gaia_server.url).get_glossary("123", scope="cn") == glossary
    assert [frame["params"] for frame in gaia_server.call_frames()][1:] == [
        {"name": "get_engagement", "arguments": {"name": "123", "scope": "cn"}},
        {"name": "get_glossary", "arguments": {"engagement_id": 42, "scope": "cn"}},
    ]


def test_session_expiry_reinitializes_and_rechecks_metadata(gaia_server: FakeGaiaServer):
    empty = {"terms": [], "vocabulary_hints": []}
    gaia_server.tools["get_glossary"] = lambda _: tool_ok(empty)
    client = GaiaClient(gaia_server.url)
    assert client.get_glossary() == empty
    gaia_server.fail_next_call_with_404 = True
    assert client.get_glossary() == empty
    assert [frame["params"]["name"] for frame in gaia_server.call_frames()] == [
        "get_server_info",
        "get_glossary",
        "get_glossary",
        "get_server_info",
        "get_glossary",
    ]
    assert sum(frame["method"] == "initialize" for frame in gaia_server.frames) == 2


def test_session_expiry_cannot_bypass_changed_contract_gate(gaia_server: FakeGaiaServer):
    gaia_server.tools["get_glossary"] = lambda _: tool_ok({"terms": [], "vocabulary_hints": []})
    client = GaiaClient(gaia_server.url)
    client.get_glossary()
    gaia_server.info["contract_version"] = "2.0.0"
    gaia_server.fail_next_call_with_404 = True
    with pytest.raises(ContractMismatchError):
        client.get_glossary()
    assert gaia_server.call_frames()[-1]["params"]["name"] == "get_server_info"


def test_http_404_is_retried_only_once(gaia_server: FakeGaiaServer):
    gaia_server.http_status = 404
    with pytest.raises(EngineUnavailableError):
        GaiaClient(gaia_server.url).get_glossary()
    assert [frame["method"] for frame in gaia_server.frames] == ["initialize", "initialize"]


def test_unreachable_server_raises_engine_unavailable():
    with pytest.raises(EngineUnavailableError, match="gaia-library unreachable"):
        GaiaClient(f"http://127.0.0.1:{free_port()}/mcp").get_server_info()


def test_actual_unknown_tool_rpc_error(gaia_server: FakeGaiaServer):
    with pytest.raises(EngineUnavailableError, match="unknown tool") as exc:
        GaiaClient(gaia_server.url).call("missing")
    assert exc.value.details["gaia_code"] == "not_found"
    assert exc.value.details["rpc_code"] == -32602


@pytest.mark.parametrize("via_rpc", [False, True])
@pytest.mark.parametrize(
    ("gaia_code", "expected"),
    [
        ("not_found", ErrorCode.NOT_FOUND),
        ("scope_denied", ErrorCode.SCOPE_DENIED),
        ("unauthorized", ErrorCode.SCOPE_DENIED),
        ("invalid_params", ErrorCode.INVALID_ARGUMENT),
        ("contract_mismatch", ErrorCode.CONTRACT_MISMATCH),
        ("conflict", ErrorCode.INVALID_ARGUMENT),
        ("busy", ErrorCode.BUSY),
        ("not_implemented", ErrorCode.ENGINE_UNAVAILABLE),
        ("internal", ErrorCode.INTERNAL),
    ],
)
def test_structured_errors_preserve_gaia_code(gaia_server, via_rpc, gaia_code, expected):
    error = {"code": gaia_code, "message": "resource not found", "details": {"scope": "cn"}}
    if via_rpc:
        gaia_server.rpc_errors["search_context"] = {
            "code": -32602,
            "message": error["message"],
            "data": error,
        }
    else:
        gaia_server.tools["search_context"] = lambda _: tool_error(
            gaia_code, error["message"], error["details"]
        )
    with pytest.raises(NarumiError) as exc:
        GaiaClient(gaia_server.url).search_context("x", scope="cn")
    assert exc.value.code == expected
    assert exc.value.message == error["message"]
    assert exc.value.details["gaia_code"] == gaia_code
    assert exc.value.details["gaia"] == {"scope": "cn"}


def test_text_only_json_result(gaia_server: FakeGaiaServer):
    result = empty_search("q")
    gaia_server.tools["search_context"] = lambda _: {
        "content": [{"type": "text", "text": json.dumps(result)}],
        "isError": False,
    }
    assert GaiaClient(gaia_server.url).search_context("q") == result


def test_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gaia_server: FakeGaiaServer):
    monkeypatch.setenv("NARUMI_HOME", str(tmp_path))
    monkeypatch.delenv(ENV_GAIA_URL, raising=False)
    monkeypatch.delenv(ENV_GAIA_API_KEY, raising=False)
    assert GaiaClient.from_env() is None
    monkeypatch.setenv(ENV_GAIA_URL, gaia_server.url)
    monkeypatch.setenv(ENV_GAIA_API_KEY, "gaia_narumi_test_12345678")
    client = GaiaClient.from_env()
    assert client is not None and client.url == gaia_server.url
    assert client.get_server_info()["client"]["name"] == "narumi"
    assert all(
        header["Authorization"] == "Bearer gaia_narumi_test_12345678"
        for header in gaia_server.headers
    )
