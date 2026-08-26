"""Transports: stdio (dev / MCP client configs) and Streamable HTTP (resident, loopback only)."""

from __future__ import annotations

import logging
from typing import Any

import anyio
import uvicorn
from mcp.server import Server
from mcp.server.stdio import stdio_server
from narumi.errors import InvalidArgumentError
from starlette.applications import Starlette

logger = logging.getLogger(__name__)

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "streamable-http"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PATH = "/mcp"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def ensure_loopback(host: str) -> str:
    """narumi has no authentication: refuse anything but a loopback bind."""
    if host not in LOOPBACK_HOSTS:
        raise InvalidArgumentError(
            f"narumi-server binds loopback interfaces only (got {host!r}); "
            f"use one of {', '.join(sorted(LOOPBACK_HOSTS))}",
            details={"host": host, "allowed": sorted(LOOPBACK_HOSTS)},
        )
    return host


async def serve_stdio(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio(server: Server[Any]) -> None:
    logger.info("serving MCP over stdio")
    anyio.run(serve_stdio, server)


def build_http_app(
    server: Server[Any],
    *,
    host: str = DEFAULT_HOST,
    path: str = DEFAULT_PATH,
    json_response: bool = True,
) -> Starlette:
    """Starlette app with the Streamable HTTP endpoint mounted at ``path``.

    ``json_response=True`` answers ``POST /mcp`` with plain ``application/json`` instead of an
    SSE stream: narumi tools never stream notifications (jobs are polled), and the menu-bar app's
    minimal JSON-RPC client stays simple. Standard MCP clients accept both.
    """
    ensure_loopback(host)
    return server.streamable_http_app(
        streamable_http_path=path, host=host, json_response=json_response
    )


def run_http(
    server: Server[Any],
    *,
    host: str = DEFAULT_HOST,
    port: int,
    path: str = DEFAULT_PATH,
    log_level: str = "info",
) -> None:
    app = build_http_app(server, host=host, path=path)
    logger.info("serving MCP over Streamable HTTP at http://%s:%d%s", host, port, path)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,  # keep our stderr logging configuration
        log_level=log_level.lower(),
        access_log=False,
    )
