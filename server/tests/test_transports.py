"""Transport tests: a real stdio subprocess and Streamable HTTP on a loopback port."""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from conftest import FAKE_RECORDER
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.server import Server
from narumi.errors import InvalidArgumentError
from narumi_server.transports import build_http_app, ensure_loopback


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_ensure_loopback():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert ensure_loopback(host) == host
    with pytest.raises(InvalidArgumentError):
        ensure_loopback("0.0.0.0")  # noqa: S104 - asserting it is refused
    with pytest.raises(InvalidArgumentError):
        build_http_app(Server("x"), host="192.168.1.10")


async def test_stdio_subprocess(home: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "narumi_server.cli",
            "--stdio",
            "--data-root",
            str(home),
            "--recorder",
            str(FAKE_RECORDER),
            "--log-level",
            "WARNING",
        ],
        env=dict(os.environ),
    )
    async with Client(params) as client:
        assert client.server_info is not None and client.server_info.name == "narumi"
        listed = await client.list_tools()
        assert len(listed.tools) == 12
        info = await client.call_tool("get_server_info", {})
        assert not info.is_error
        assert info.structured_content["capabilities"]["transports"] == ["stdio"]
        assert info.structured_content["capabilities"]["recording"] is True
        bad = await client.call_tool("get_meeting", {"meeting_id": "nope"})
        assert bad.is_error
        assert bad.structured_content["error"]["code"] == "invalid_argument"
        empty = await client.call_tool("list_meetings", {})
        assert empty.structured_content == {"meetings": []}


async def test_streamable_http(server: Server[Any]):
    port = free_port()
    app = build_http_app(server, host="127.0.0.1")
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, log_level="warning", access_log=False
    )
    http_server = uvicorn.Server(config)
    thread = threading.Thread(target=http_server.run, name="uvicorn-test", daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if http_server.started:
                break
            await anyio.sleep(0.05)
        assert http_server.started, "uvicorn did not start"
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools][:2] == ["get_server_info", "start_recording"]
            info = await client.call_tool("get_server_info", {})
            assert not info.is_error and info.structured_content["name"] == "narumi"
            bad = await client.call_tool("get_job_status", {"job_id": "job-000000000000"})
            assert bad.is_error and bad.structured_content["error"]["code"] == "not_found"
    finally:
        http_server.should_exit = True
        thread.join(timeout=15)
