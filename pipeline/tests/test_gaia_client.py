"""GaiaClient against an in-process fake gaia-library MCP server (http.server, no mcp package).

``FakeGaiaServer`` speaks just enough Streamable HTTP MCP for the client: JSON-RPC over POST,
``initialize`` / ``notifications/initialized`` / ``tools/call``, a ``Mcp-Session-Id`` header,
plain JSON responses (or SSE when ``sse=True``). ``test_export_gaia`` reuses it.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from narumi.errors import EngineUnavailableError, ErrorCode, NarumiError
from narumi.gaia import ENV_GAIA_URL, GaiaClient

SESSION_ID = "sess-1234"


def tool_ok(structured: dict[str, Any]) -> dict[str, Any]:
    """A successful MCP tools/call result carrying ``structured`` as structuredContent."""
    return {
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
        "structuredContent": structured,
        "isError": False,
    }


def tool_error(code: str, message: str) -> dict[str, Any]:
    payload = {"error": {"code": code, "message": message}}
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": payload,
        "isError": True,
    }


class FakeGaiaServer:
    """Bounded-lifetime fake gaia-library endpoint. ``tools`` maps name → fn(args) → result."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.tools: dict[str, Any] = {}
        self.sse = False
        self.fail_next_call_with_404 = False
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                server.frames.append(body)
                server.headers.append(dict(self.headers.items()))
                method = body.get("method")
                if "id" not in body:  # notification
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "initialize":
                    self._reply(
                        body["id"],
                        {
                            "result": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "fake-gaia", "version": "0"},
                            }
                        },
                        session=SESSION_ID,
                    )
                    return
                if method == "tools/call":
                    if server.fail_next_call_with_404:
                        server.fail_next_call_with_404 = False
                        raw = b"session not found"
                        self.send_response(404)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    name = body["params"]["name"]
                    fn = server.tools.get(name)
                    if fn is None:
                        self._reply(
                            body["id"],
                            {"error": {"code": -32602, "message": f"Unknown tool: {name}"}},
                        )
                        return
                    self._reply(body["id"], {"result": fn(body["params"].get("arguments") or {})})
                    return
                self._reply(
                    body["id"],
                    {"error": {"code": -32601, "message": f"Method not found: {method}"}},
                )

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
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def call_frames(self) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("method") == "tools/call"]


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


# ---------------------------------------------------------------------------- protocol
def test_initialize_handshake_and_session_header(gaia_server: FakeGaiaServer):
    gaia_server.tools["search_context"] = lambda args: tool_ok({"references": []})
    client = GaiaClient(gaia_server.url)
    assert client.search_context("定例", engagement="acme") == []

    methods = [frame.get("method") for frame in gaia_server.frames]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    init = gaia_server.frames[0]
    assert init["params"]["protocolVersion"] == "2025-06-18"
    assert init["params"]["clientInfo"]["name"] == "narumi-pipeline"
    assert "id" not in gaia_server.frames[1]
    call = gaia_server.frames[2]
    assert call["params"] == {
        "name": "search_context",
        "arguments": {"query": "定例", "engagement": "acme"},
    }
    # the session id from initialize is echoed on the tools/call request
    assert gaia_server.headers[2].get("Mcp-Session-Id") == SESSION_ID

    # a second call reuses the session: exactly one more frame, still with the session header
    client.search_context("次回")
    assert [f.get("method") for f in gaia_server.frames] == [*methods, "tools/call"]
    assert gaia_server.headers[3].get("Mcp-Session-Id") == SESSION_ID


def test_sse_response_body(gaia_server: FakeGaiaServer):
    gaia_server.sse = True
    gaia_server.tools["get_glossary"] = lambda args: tool_ok(
        {"terms": [{"term": "SCIM", "aliases": ["scim"]}]}
    )
    client = GaiaClient(gaia_server.url)
    assert client.get_glossary("acme") == [{"term": "SCIM", "aliases": ["scim"]}]


def test_session_expiry_reinitializes_once(gaia_server: FakeGaiaServer):
    gaia_server.tools["get_glossary"] = lambda args: tool_ok({"terms": []})
    client = GaiaClient(gaia_server.url)
    assert client.get_glossary() == []
    gaia_server.fail_next_call_with_404 = True
    assert client.get_glossary() == []
    methods = [f.get("method") for f in gaia_server.frames]
    # first call handshake + call, then 404'd call, re-handshake, retried call
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


# ---------------------------------------------------------------------------- errors
def test_unreachable_server_raises_engine_unavailable():
    client = GaiaClient(f"http://127.0.0.1:{free_port()}/mcp")
    with pytest.raises(EngineUnavailableError) as excinfo:
        client.call("search_context", {"query": "x"})
    assert "gaia-library unreachable" in str(excinfo.value)


def test_unknown_tool_raises_engine_unavailable_with_server_message(
    gaia_server: FakeGaiaServer,
):
    client = GaiaClient(gaia_server.url)
    with pytest.raises(EngineUnavailableError) as excinfo:
        client.call("no_such_tool")
    assert "Unknown tool: no_such_tool" in str(excinfo.value)


def test_tool_error_keeps_structured_code(gaia_server: FakeGaiaServer):
    gaia_server.tools["search_context"] = lambda args: tool_error("not_found", "no such scope")
    client = GaiaClient(gaia_server.url)
    with pytest.raises(NarumiError) as excinfo:
        client.search_context("x", scope="missing")
    assert excinfo.value.code == ErrorCode.NOT_FOUND
    assert excinfo.value.message == "no such scope"


# ---------------------------------------------------------------------------- typed helpers
def test_typed_helpers_and_result_tolerance(gaia_server: FakeGaiaServer):
    gaia_server.tools["resolve_speakers"] = lambda args: tool_ok(
        {"speakers": {"tanaka": {"name": "田中太郎"}}}
    )
    gaia_server.tools["propose_update"] = lambda args: tool_ok(
        {"proposal_id": "prop-1", "status": "queued"}
    )
    client = GaiaClient(gaia_server.url)

    speakers = client.resolve_speakers(["tanaka"], engagement="acme")
    assert speakers == {"tanaka": {"name": "田中太郎"}}

    result = client.propose_update(
        entity_type="interaction",
        patch={"kind": "meeting_minutes"},
        scope="client-a",
        provenance="minutes://meeting/x",
        request_id="req-1",
    )
    assert result == {"proposal_id": "prop-1", "status": "queued"}
    frame = gaia_server.call_frames()[-1]
    assert frame["params"]["name"] == "propose_update"
    assert frame["params"]["arguments"] == {
        "entity_type": "interaction",
        "patch": {"kind": "meeting_minutes"},
        "scope": "client-a",
        "provenance": "minutes://meeting/x",
        "request_id": "req-1",
    }


def test_text_only_result_is_parsed_as_json(gaia_server: FakeGaiaServer):
    gaia_server.tools["search_context"] = lambda args: {
        "content": [{"type": "text", "text": json.dumps({"references": [{"uri": "u"}]})}],
        "isError": False,
    }
    client = GaiaClient(gaia_server.url)
    assert client.search_context("q") == [{"uri": "u"}]


# ---------------------------------------------------------------------------- construction
def test_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV_GAIA_URL, raising=False)
    assert GaiaClient.from_env() is None
    monkeypatch.setenv(ENV_GAIA_URL, "http://127.0.0.1:1/mcp")
    client = GaiaClient.from_env()
    assert client is not None and client.url == "http://127.0.0.1:1/mcp"
