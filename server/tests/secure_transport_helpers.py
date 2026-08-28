"""Real loopback TLS fixtures backed exclusively by an in-memory fake credential store."""

from __future__ import annotations

import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import uvicorn
from mcp.server import Server
from narumi_server.secure_transport import (
    ClientTransport,
    ServerTransport,
    load_client_transport,
    prepare_server_transport,
)
from narumi_server.transports import build_http_app
from starlette.types import ASGIApp

FAKE_HTTP_CLI = """
import json
import os
import sys
from narumi_server import cli, secure_transport

pipe = int(sys.argv.pop(1))

class TestSecretStore:
    def get(self, account):
        raise AssertionError("No real credential lookup is allowed")
    def set(self, account, value):
        os.write(pipe, (json.dumps({"account": account, "value": value}) + "\\n").encode())
        os.close(pipe)
    def delete(self, account):
        pass

secure_transport._default_secret_store = TestSecretStore
cli.cli(prog_name="narumi-server")
"""


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        self.values[account] = value

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def serve_tls(app: ASGIApp, credentials: ServerTransport) -> AsyncIterator[None]:
    from urllib.parse import urlsplit

    port = urlsplit(credentials.url).port
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(credentials.certificate_path),
            ssl_keyfile=str(credentials.private_key_path),
            proxy_headers=False,
            forwarded_allow_ips="",
            log_config=None,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=server.run, name="tls-test", daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            await anyio.sleep(0.01)
        assert server.started, "TLS test server did not start"
        yield
    finally:
        server.should_exit = True
        await anyio.to_thread.run_sync(thread.join, 5)
        assert not thread.is_alive(), "TLS test server did not stop"


@asynccontextmanager
async def running_server(
    server: Server[Any], root: Path
) -> AsyncIterator[tuple[ServerTransport, ClientTransport]]:
    store = MemorySecretStore()
    with prepare_server_transport(
        root, str(uuid4()), port=free_port(), secret_store=store
    ) as credentials:
        app = build_http_app(server, credentials=credentials)
        async with serve_tls(app, credentials):
            yield credentials, load_client_transport(root, secret_store=store)
