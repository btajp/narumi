"""The public stdio bridge forwards to the resident server and never starts another context."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import mcp_types
import pytest
from click.testing import CliRunner
from mcp.server import Server
from narumi_server.cli import cli
from narumi_server.secure_transport import ServerTransport
from narumi_server.transport_auth import safe_rpc_message
from secure_transport_helpers import running_server

FAKE_SECRET = "fake-stdio-bridge-key-5632"
BRIDGE_CLI = """
import json
import os
import sys
from narumi_server import cli, secure_transport

with os.fdopen(int(sys.argv.pop(1)), "rb") as pipe:
    credential = json.load(pipe)

class TestSecretStore:
    def get(self, account):
        return credential["value"] if account == credential["account"] else None
    def set(self, account, value):
        raise AssertionError("The bridge must not create credentials")
    def delete(self, account):
        raise AssertionError("The bridge must not delete credentials")

secure_transport._default_secret_store = TestSecretStore
cli.cli(prog_name="narumi-server")
"""
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


@contextmanager
def bridge_process(
    home: Path, server: ServerTransport, *, token: str | None = None
) -> Iterator[tuple[subprocess.Popen[bytes], list[bytes]]]:
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            BRIDGE_CLI,
            str(read_fd),
            "--stdio-bridge",
            "--data-root",
            str(home),
            "--log-level",
            "DEBUG",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    diagnostics: list[bytes] = []

    def drain_diagnostics() -> None:
        assert proc.stderr is not None
        diagnostics.extend(iter(proc.stderr.readline, b""))

    drain = threading.Thread(target=drain_diagnostics, daemon=True)
    drain.start()
    try:
        with os.fdopen(write_fd, "wb") as pipe:
            pipe.write(
                json.dumps(
                    {"account": server.token_account, "value": token or server.client_token}
                ).encode()
            )
        yield proc, diagnostics
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        drain.join(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def send_request(proc: subprocess.Popen[bytes], document: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(document) + "\n").encode())
    proc.stdin.flush()


def read_response(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 5)
    if not ready:
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("stdio bridge did not return an MCP response")
    line = proc.stdout.readline()
    assert line, "stdio bridge exited before returning an MCP response"
    return json.loads(line)


async def test_stdio_bridge_roundtrip_and_eof(home: Path):
    calls = []

    async def list_tools(_ctx, _params):
        return mcp_types.ListToolsResult(
            tools=[mcp_types.Tool(name="probe", input_schema={"type": "object"})]
        )

    async def call_tool(_ctx, params):
        calls.append(params.arguments)
        return mcp_types.CallToolResult(content=[], structured_content={"ok": True})

    async with running_server(
        Server("bridge-target", on_list_tools=list_tools, on_call_tool=call_tool), home
    ) as (server, _):
        with bridge_process(home, server) as (proc, diagnostics):
            send_request(proc, INITIALIZE)
            initialized = await anyio.to_thread.run_sync(read_response, proc)
            assert initialized["id"] == 1 and "result" in initialized
            send_request(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            send_request(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listed = await anyio.to_thread.run_sync(read_response, proc)
            assert [tool["name"] for tool in listed["result"]["tools"]] == ["probe"]
            send_request(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "probe", "arguments": {"api_key": FAKE_SECRET}},
                },
            )
            response = await anyio.to_thread.run_sync(read_response, proc)
            assert response["result"]["structuredContent"] == {"ok": True}
            assert proc.stdin is not None
            proc.stdin.close()
            code = await anyio.to_thread.run_sync(proc.wait, 5)
            assert code == 0
        output = b"".join(diagnostics).decode("utf-8")
        assert FAKE_SECRET not in output
        assert server.client_token not in output
        assert server.bootstrap_path.exists(), "closing a bridge must not close the resident server"
    assert calls == [{"api_key": FAKE_SECRET}]


@pytest.mark.parametrize("bad_token", [False, True])
async def test_bridge_failure_exits_without_waiting_for_stdin_eof(home: Path, bad_token: bool):
    async with running_server(Server("failure-target"), home) as (server, _):
        token = "wrong-transport-token-" * 3 if bad_token else None
        with bridge_process(home, server, token=token) as (proc, diagnostics):
            request = (
                INITIALIZE
                if bad_token
                else {"jsonrpc": "2.0", "id": 1, "method": {"api_key": FAKE_SECRET}}
            )
            send_request(proc, request)
            assert proc.stdin is not None and not proc.stdin.closed
            assert await anyio.to_thread.run_sync(proc.wait, 5) == 2
        output = b"".join(diagnostics).decode("utf-8")
        assert "authentication_required" in output
        assert FAKE_SECRET not in output and server.client_token not in output
        assert server.bootstrap_path.exists()


def test_absent_bootstrap_never_builds_an_independent_context(home: Path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("--stdio-bridge must not create an in-process server")

    monkeypatch.setattr("narumi_server.cli.build_context", forbidden)
    result = CliRunner().invoke(cli, ["--stdio-bridge", "--data-root", str(home)])
    assert result.exit_code == 2
    assert "No authenticated local server bootstrap" in result.stderr


def test_bridge_parser_errors_keep_ids_but_withhold_input_values():
    message = mcp_types.JSONRPCError(
        jsonrpc="2.0",
        id=7,
        error=mcp_types.ErrorData(code=-32603, message=FAKE_SECRET, data={"input": FAKE_SECRET}),
    )
    safe = safe_rpc_message(message)
    assert safe.id == 7 and safe.error.code == -32603
    assert FAKE_SECRET not in safe.model_dump_json()
