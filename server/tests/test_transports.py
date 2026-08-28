"""Transport tests: a real stdio subprocess and Streamable HTTP on a loopback port."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from conftest import FAKE_RECORDER
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.server import Server
from narumi.contracts.loader import load_contracts
from narumi.errors import InvalidArgumentError
from narumi_server.transports import (
    ShutdownRequested,
    build_http_app,
    ensure_loopback,
    graceful_sigterm,
)


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
        assert len(listed.tools) == len(load_contracts().tool_names())
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
            assert [t.name for t in listed.tools][:3] == [
                "get_server_info",
                "configure_recording_permission",
                "start_recording",
            ]
            info = await client.call_tool("get_server_info", {})
            assert not info.is_error and info.structured_content["name"] == "narumi"
            bad = await client.call_tool("get_job_status", {"job_id": "job-000000000000"})
            assert bad.is_error and bad.structured_content["error"]["code"] == "not_found"
    finally:
        http_server.should_exit = True
        thread.join(timeout=15)


def test_graceful_sigterm_raises_once_then_ignores():
    """The handler unwinds the first SIGTERM as an exception and swallows repeats (no signal is
    sent to another process: ``raise_signal`` delivers it to this thread synchronously)."""
    before = signal.getsignal(signal.SIGTERM)
    with graceful_sigterm():
        assert signal.getsignal(signal.SIGTERM) is not before
        with pytest.raises(ShutdownRequested) as caught:
            signal.raise_signal(signal.SIGTERM)
        assert caught.value.signum == signal.SIGTERM
        assert "SIGTERM" in str(caught.value)
        signal.raise_signal(signal.SIGTERM)  # while finalizing: ignored, must not raise
    assert signal.getsignal(signal.SIGTERM) is before
    assert not issubclass(ShutdownRequested, Exception)  # `except Exception` must not eat it


async def wait_for_http(url: str, proc: subprocess.Popen[bytes], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"narumi-server exited early with {proc.returncode}")
        try:
            async with Client(url) as client:
                await client.list_tools()
            return
        except Exception:  # noqa: BLE001 - not listening yet
            await anyio.sleep(0.1)
    raise AssertionError("narumi-server did not start listening")


async def test_http_sigterm_finalizes_recording(home: Path, tmp_path: Path):
    """What narumi.app does on quit: SIGTERM to ``narumi-server --http`` while recording.

    uvicorn re-raises the captured SIGTERM after its graceful shutdown; without the CLI's
    handler that killed the process before ``ctx.close()``, leaving the meeting in status
    ``recording`` with no ``stopped_at`` (observed on 2026-08-27). The server must instead exit
    0 with the recording finalized and the recorder process gone.
    """
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "narumi_server.cli",
                "--http",
                "--port",
                str(port),
                "--data-root",
                str(home),
                "--recorder",
                str(FAKE_RECORDER),
                "--log-level",
                "INFO",
            ],
            env=dict(os.environ),
            stdout=log,
            stderr=log,
        )
        try:
            await wait_for_http(url, proc)
            async with Client(url) as client:
                started = await client.call_tool(
                    "start_recording",
                    {"meeting_name": "SIGTERM 中の会議", "request_id": str(uuid.uuid4())},
                )
                assert not started.is_error, started.structured_content
                meeting_id = started.structured_content["meeting_id"]
            proc.send_signal(signal.SIGTERM)
            returncode = await anyio.to_thread.run_sync(proc.wait, 40)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    text = log_path.read_text(encoding="utf-8")
    assert returncode == 0, text
    assert "received SIGTERM; shutting down" in text
    assert f"finalized recording {meeting_id} at shutdown" in text
    manifest = json.loads((home / "meetings" / meeting_id / "manifest.json").read_text("utf-8"))
    assert manifest["status"] == "recorded"
    assert manifest["recording"]["stopped_at"] is not None
    assert (home / "meetings" / meeting_id / "tracks" / "recorder.json").is_file()
