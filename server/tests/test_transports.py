"""Transport tests: a real stdio subprocess and Streamable HTTP on a loopback port."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from click.testing import CliRunner
from conftest import FAKE_RECORDER
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.server import Server
from narumi.contracts.loader import load_contracts
from narumi.errors import BusyError, InvalidArgumentError
from narumi_server import cli as server_cli
from narumi_server.secure_transport import (
    ClientTransport,
    acquire_server_lease,
    load_client_transport,
)
from narumi_server.stdio_bridge import authenticated_streams
from narumi_server.transports import (
    ShutdownRequested,
    build_http_app,
    ensure_loopback,
    graceful_sigterm,
)
from secure_transport_helpers import FAKE_HTTP_CLI, MemorySecretStore, free_port, running_server


def test_ensure_loopback():
    for host in ("127.0.0.1", "::1"):
        assert ensure_loopback(host) == host
    with pytest.raises(InvalidArgumentError):
        ensure_loopback("localhost")
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


async def test_streamable_http(server: Server[Any], home: Path):
    async with running_server(server, home) as (_, connection):
        async with Client(authenticated_streams(connection)) as client:
            listed = await client.list_tools()
            assert {
                "get_server_info",
                "configure_recording_permission",
                "start_recording",
            }.issubset({tool.name for tool in listed.tools})
            info = await client.call_tool("get_server_info", {})
            assert not info.is_error and info.structured_content["name"] == "narumi"
            bad = await client.call_tool("get_job_status", {"job_id": "job-000000000000"})
            assert bad.is_error and bad.structured_content["error"]["code"] == "not_found"


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


async def wait_for_http(
    root: Path, store: MemorySecretStore, proc: subprocess.Popen[bytes], timeout: float = 30.0
) -> ClientTransport:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"narumi-server exited early with {proc.returncode}")
        try:
            connection = load_client_transport(root, secret_store=store)
            async with Client(authenticated_streams(connection)) as client:
                await client.list_tools()
            return connection
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
    store = MemorySecretStore()
    read_fd, write_fd = os.pipe()
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                FAKE_HTTP_CLI,
                str(write_fd),
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
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        try:
            ready, _, _ = await anyio.to_thread.run_sync(select.select, [read_fd], [], [], 10)
            assert ready, "test server did not publish its fake credential through the private pipe"
            with os.fdopen(read_fd, "rb") as pipe:
                credential = json.loads(pipe.readline())
            store.set(credential["account"], credential["value"])
            connection = await wait_for_http(home, store, proc)
            async with Client(authenticated_streams(connection)) as client:
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


@pytest.mark.parametrize("mode", ["--http", "--stdio"])
def test_second_server_cannot_construct_context_and_mark_live_jobs_stale(home, monkeypatch, mode):
    def forbidden(*_args, **_kwargs):
        pytest.fail("a second server must acquire the lease before building its context")

    monkeypatch.setattr(server_cli, "build_context", forbidden)
    with acquire_server_lease(home):
        result = CliRunner().invoke(server_cli.cli, [mode, "--data-root", str(home)])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "busy"


@pytest.mark.parametrize("mode", ["--http", "--stdio"])
def test_cli_keeps_lease_through_context_shutdown(home, monkeypatch, mode):
    events = []
    store = MemorySecretStore()
    monkeypatch.setattr("narumi_server.secure_transport._default_secret_store", lambda: store)

    def context(root, **options):
        with pytest.raises(BusyError):
            acquire_server_lease(root)
        events.append("context")

        def close():
            with pytest.raises(BusyError):
                acquire_server_lease(root)
            events.append("close")

        return SimpleNamespace(
            data_root=root, server_instance_id=options["server_instance_id"], close=close
        )

    def run(*_args, **kwargs):
        if mode == "--http":
            assert kwargs["credentials"].server_instance_id == kwargs["server_instance_id"]
        events.append("serve")

    monkeypatch.setattr(server_cli, "build_context", context)
    monkeypatch.setattr(server_cli, "build_server", lambda _ctx: None)
    monkeypatch.setattr(server_cli, "run_http", run)
    monkeypatch.setattr(server_cli, "run_stdio", run)
    result = CliRunner().invoke(server_cli.cli, [mode, "--data-root", str(home)])
    assert result.exit_code == 0, result.output
    assert events == ["context", "serve", "close"]
    assert store.values == {}
    with acquire_server_lease(home):
        pass
