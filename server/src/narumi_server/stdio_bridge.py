"""Transparent MCP stdio bridge to the authenticated resident server; no local fallback."""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from narumi_server.secure_transport import ClientTransport, load_client_transport
from narumi_server.transport_auth import safe_rpc_message
from narumi_server.transport_errors import (
    SecureTransportUnavailableError,
    TransportSecurityError,
)
from narumi_server.transport_logging import install_transport_log_filters

if TYPE_CHECKING:
    from narumi.providers.secrets import SecretStore


@asynccontextmanager
async def authenticated_streams(connection: ClientTransport) -> AsyncIterator[Any]:
    """Supply our own TLS-only client: MCP defaults permit redirects and environment proxies."""
    try:
        import httpx2
    except ImportError:
        raise SecureTransportUnavailableError() from None
    install_transport_log_filters()

    async def check_request(request: Any) -> None:
        if str(request.url) != connection.url:
            raise TransportSecurityError()

    async def check_response(response: Any) -> None:
        if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
            raise TransportSecurityError()

    async with httpx2.AsyncClient(
        verify=connection.ssl_context,
        headers={"Authorization": f"Bearer {connection.client_token}"},
        follow_redirects=False,
        trust_env=False,
        timeout=httpx2.Timeout(30.0, read=300.0),
        event_hooks={"request": [check_request], "response": [check_response]},
    ) as http_client:
        async with streamable_http_client(connection.url, http_client=http_client) as streams:
            yield streams


async def forward_streams(
    local_read: Any, local_write: Any, remote_read: Any, remote_write: Any
) -> None:
    """Forward frames once, retaining protocol IDs; malformed frames never become diagnostics."""
    async with anyio.create_task_group() as group:

        async def forward(source: Any, destination: Any, *, sanitize: bool) -> None:
            async for frame in source:
                if isinstance(frame, Exception):
                    raise TransportSecurityError() from None
                if sanitize:
                    frame = SessionMessage(safe_rpc_message(frame.message), metadata=frame.metadata)
                await destination.send(frame)
            group.cancel_scope.cancel()

        async def outbound() -> None:
            await forward(local_read, remote_write, sanitize=False)

        async def inbound() -> None:
            await forward(remote_read, local_write, sanitize=True)

        group.start_soon(outbound)
        group.start_soon(inbound)


async def serve_stdio_bridge(connection: ClientTransport) -> None:
    reader = _PipeLines(os.dup(0))
    try:
        async with (
            authenticated_streams(connection) as (remote_read, remote_write),
            stdio_server(stdin=reader) as (local_read, local_write),
        ):
            try:
                await forward_streams(local_read, local_write, remote_read, remote_write)
            finally:
                await local_write.aclose()
    except Exception:
        # HTTP client exceptions may include URLs, response bodies or malformed input values.
        raise TransportSecurityError() from None
    finally:
        os.close(reader.fd)


class _PipeLines:
    """A cancellable stdin reader: a remote failure must not wait for a blocking input thread."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        os.set_blocking(fd, False)

    async def __aiter__(self) -> AsyncIterator[str]:
        buffer = bytearray()
        regular = stat.S_ISREG(os.fstat(self.fd).st_mode)
        while True:
            if not regular:
                await anyio.wait_readable(self.fd)
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                if buffer:
                    yield buffer.decode("utf-8")
                return
            buffer.extend(chunk)
            if len(buffer) > 4 * 1024 * 1024:
                raise TransportSecurityError()
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                yield line.decode("utf-8") + "\n"


def run_stdio_bridge(
    root: Path,
    *,
    expected_url: str | None = None,
    secret_store: SecretStore | None = None,
) -> None:
    install_transport_log_filters()
    connection = load_client_transport(root, expected_url=expected_url, secret_store=secret_store)
    anyio.run(serve_stdio_bridge, connection)
