"""Transports: developer stdio and authenticated, pinned loopback TLS for resident clients."""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

import anyio
import uvicorn
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from narumi.errors import InvalidArgumentError
from starlette.applications import Starlette

from narumi_server.secure_transport import ServerTransport, prepare_server_transport
from narumi_server.transport_auth import LocalAuthenticationMiddleware
from narumi_server.transport_errors import TransportSecurityError
from narumi_server.transport_logging import install_transport_log_filters
from narumi_server.transport_tls import NUMERIC_LOOPBACK_HOSTS, endpoint_for

if TYPE_CHECKING:
    from narumi.providers.secrets import SecretStore

logger = logging.getLogger(__name__)

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "streamable-http"
TRANSPORT_STDIO_BRIDGE = "stdio-bridge"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PATH = "/mcp"
LOOPBACK_HOSTS = NUMERIC_LOOPBACK_HOSTS
GRACEFUL_SHUTDOWN_TIMEOUT = 10
"""Seconds uvicorn waits for open connections at shutdown before cancelling them.

An MCP client's ``GET /mcp`` event stream never ends on its own; without a bound a SIGTERM would
wait until every client disconnects, and narumi.app would escalate to SIGKILL after its own
timeout — skipping the recording finalization in :meth:`ServerContext.close`."""


class ShutdownRequested(BaseException):
    """SIGTERM asked the server to stop (raised on the main thread by :func:`graceful_sigterm`).

    A ``BaseException`` like ``KeyboardInterrupt`` so that ordinary ``except Exception`` blocks
    inside the server do not swallow it.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum

    def __str__(self) -> str:
        return f"received {signal.Signals(self.signum).name}"


@contextlib.contextmanager
def graceful_sigterm() -> Iterator[None]:
    """Turn the first SIGTERM into :class:`ShutdownRequested`; ignore any later one.

    Why this exists: uvicorn installs its own SIGTERM / SIGINT handlers while serving (graceful
    shutdown), restores the previous handlers afterwards and then *re-raises* the captured
    signal so a parent sees the usual exit status. With SIGTERM's default action that kills the
    process inside ``Server.run()`` — before the CLI's ``finally: ctx.close()`` could finalize a
    running recording. With this handler installed *before* uvicorn runs, the re-raise lands
    here instead and unwinds as an exception, so ``finally`` blocks run. A second SIGTERM while
    the recording is being finalized is ignored (narumi.app escalates to SIGKILL after its own
    timeout; that is the only way to cut the finalization short). SIGINT is untouched.

    Signal handlers can only be changed on the main thread; elsewhere this is a no-op (uvicorn
    then does not capture signals either).
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    raised = False

    def handler(signum: int, _frame: FrameType | None) -> None:
        nonlocal raised
        if raised:
            logger.info("ignoring repeated %s while shutting down", signal.Signals(signum).name)
            return
        raised = True
        raise ShutdownRequested(signum)

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def ensure_loopback(host: str) -> str:
    """Keep authenticated local transport on a numeric address, without DNS resolution."""
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
    install_transport_log_filters()
    logger.info("serving MCP over stdio")
    anyio.run(serve_stdio, server)


def build_http_app(
    server: Server[Any],
    *,
    host: str = DEFAULT_HOST,
    path: str = DEFAULT_PATH,
    json_response: bool = True,
    credentials: ServerTransport | None = None,
) -> Starlette:
    """Starlette app with the Streamable HTTP endpoint mounted at ``path``.

    ``json_response=True`` answers ``POST /mcp`` with plain ``application/json`` instead of an
    SSE stream: narumi tools never stream notifications (jobs are polled), and the menu-bar app's
    minimal JSON-RPC client stays simple. Standard MCP clients accept both.
    """
    ensure_loopback(host)
    if credentials is None:
        raise TransportSecurityError()
    install_transport_log_filters()
    from urllib.parse import urlsplit

    endpoint = urlsplit(credentials.url)
    if endpoint.hostname != host or endpoint.path != path:
        raise TransportSecurityError()
    app = server.streamable_http_app(
        streamable_http_path=path,
        host=host,
        json_response=json_response,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[endpoint.netloc],
            allowed_origins=[f"https://{endpoint.netloc}"],
        ),
    )
    app.add_middleware(
        LocalAuthenticationMiddleware,
        url=credentials.url,
        client_token=credentials.client_token,
    )
    return app


def run_http(
    server: Server[Any],
    *,
    host: str = DEFAULT_HOST,
    port: int,
    path: str = DEFAULT_PATH,
    log_level: str = "info",
    data_root: Path,
    server_instance_id: str,
    secret_store: SecretStore | None = None,
    credentials: ServerTransport | None = None,
) -> None:
    """Serve until SIGINT (``KeyboardInterrupt``) or SIGTERM (:class:`ShutdownRequested`).

    Both unwind as exceptions *after* uvicorn's graceful shutdown (lifespan closed, connections
    cancelled after :data:`GRACEFUL_SHUTDOWN_TIMEOUT`), so the caller's ``finally`` can close
    the :class:`ServerContext` — finalizing a running recording — with no request in flight.
    SIGTERM only unwinds this way inside :func:`graceful_sigterm`; the CLI installs it.
    """
    prepared = (
        contextlib.nullcontext(credentials)
        if credentials is not None
        else prepare_server_transport(data_root, server_instance_id, host, port, path, secret_store)
    )
    with prepared as credentials:
        if credentials.server_instance_id != server_instance_id or credentials.url != endpoint_for(
            host, port, path
        ):
            raise TransportSecurityError()
        app = build_http_app(server, host=host, path=path, credentials=credentials)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            ssl_certfile=str(credentials.certificate_path),
            ssl_keyfile=str(credentials.private_key_path),
            proxy_headers=False,
            forwarded_allow_ips="",
            log_config=None,  # keep our stderr logging configuration
            log_level=log_level.lower(),
            access_log=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT,
        )
        logger.info("serving authenticated MCP at %s", credentials.url)
        uvicorn.Server(config).run()
