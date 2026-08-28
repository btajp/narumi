"""``narumi-server`` console script.

    narumi-server --stdio                                # isolated developer server
    narumi-server --stdio-bridge                         # MCP client → resident TLS server
    narumi-server --http --host 127.0.0.1 --port 8765      # authenticated TLS at /mcp
    narumi-server --data-root ~/narumi-data --stdio        # explicit data root (NARUMI_HOME)

Logging goes to stderr (stdout is the MCP stream in stdio mode). Start-up failures such as a
contract / handler mismatch print the structured error as JSON on stderr and exit with code 2.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from pathlib import Path
from uuid import uuid4

import click
from narumi.config import DEFAULT_HTTP_PORT, ENV_HOME, ENV_RECORDER
from narumi.config import data_root as resolve_data_root
from narumi.errors import NarumiError

from narumi_server import __version__
from narumi_server.app import build_server
from narumi_server.context import ENV_VALIDATE_OUTPUT, build_context
from narumi_server.secure_transport import acquire_server_lease, prepare_server_transport
from narumi_server.transport_logging import install_transport_log_filters
from narumi_server.transports import (
    DEFAULT_HOST,
    DEFAULT_PATH,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    TRANSPORT_STDIO_BRIDGE,
    ShutdownRequested,
    ensure_loopback,
    graceful_sigterm,
    run_http,
    run_stdio,
)

ERROR_EXIT_CODE = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT, stream=sys.stderr, force=True)
    install_transport_log_filters()


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="narumi-server")
@click.option("--stdio", "transport", flag_value=TRANSPORT_STDIO, help="Serve over stdio.")
@click.option(
    "--stdio-bridge",
    "transport",
    flag_value=TRANSPORT_STDIO_BRIDGE,
    help="Bridge MCP stdin/stdout to the authenticated resident server (no fallback).",
)
@click.option(
    "--http",
    "transport",
    flag_value=TRANSPORT_HTTP,
    help="Serve authenticated Streamable HTTP over TLS (mounted at --path, loopback only).",
)
@click.option("--server-url", default=None, help="Expected resident TLS URL (--stdio-bridge only).")
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="Bind address (loopback).")
@click.option("--port", default=DEFAULT_HTTP_PORT, show_default=True, type=int, help="HTTP port.")
@click.option("--path", default=DEFAULT_PATH, show_default=True, help="HTTP endpoint path.")
@click.option(
    "--data-root",
    type=click.Path(file_okay=False, path_type=Path),
    envvar=ENV_HOME,
    show_envvar=True,
    default=None,
    help="Data root with meetings/ and narumi.db (default: ~/Library/Application Support/narumi).",
)
@click.option(
    "--recorder",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar=ENV_RECORDER,
    show_envvar=True,
    default=None,
    help="narumi-recorder binary (default: app/.build/{release,debug}/narumi-recorder).",
)
@click.option(
    "--contracts-dir",
    type=click.Path(file_okay=False, path_type=Path),
    envvar="NARUMI_CONTRACTS_DIR",
    show_envvar=True,
    default=None,
    help="Contract files directory (default: the repository checkout).",
)
@click.option(
    "--validate-output",
    is_flag=True,
    envvar=ENV_VALIDATE_OUTPUT,
    show_envvar=True,
    help="Validate every tool result against its outputSchema (slower; on in tests).",
)
@click.option(
    "--log-level",
    default="INFO",
    show_default=True,
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
)
def cli(
    transport: str | None,
    server_url: str | None,
    host: str,
    port: int,
    path: str,
    data_root: Path | None,
    recorder: Path | None,
    contracts_dir: Path | None,
    validate_output: bool,
    log_level: str,
) -> None:
    """narumi MCP server: contract-driven tools over stdio or Streamable HTTP."""
    configure_logging(log_level)
    log = logging.getLogger("narumi_server")
    if transport is None:
        transport = TRANSPORT_STDIO
        log.info("no transport given; defaulting to --stdio")
    # --http: SIGTERM (what narumi.app sends on quit) unwinds as ShutdownRequested after
    # uvicorn's graceful shutdown and stays handled through ctx.close(), so a running recording
    # is finalized (see transports.graceful_sigterm). --stdio keeps the default SIGTERM action:
    # its stdin reader thread cannot be interrupted, and MCP clients end it by closing stdin.
    sigterm = graceful_sigterm() if transport == TRANSPORT_HTTP else contextlib.nullcontext()
    with sigterm:
        ctx = None
        lease = None
        credentials = None
        try:
            if transport == TRANSPORT_STDIO_BRIDGE:
                from narumi_server.stdio_bridge import run_stdio_bridge

                run_stdio_bridge(resolve_data_root(data_root), expected_url=server_url)
                return
            if server_url is not None:
                raise click.UsageError("--server-url is only valid with --stdio-bridge")
            root = resolve_data_root(data_root)
            instance_id = str(uuid4())
            if transport == TRANSPORT_HTTP:
                ensure_loopback(host)
                credentials = prepare_server_transport(root, instance_id, host, port, path)
            else:
                lease = acquire_server_lease(root)
            # JobManager performs recovery writes at construction. Acquire the root lease
            # before this point, and hold it through close(), including shutdown recovery.
            ctx = build_context(
                root,
                recorder_path=recorder,
                contracts_dir=contracts_dir,
                transports=[transport],
                validate_output=validate_output or None,
                server_instance_id=instance_id,
                recover_jobs=True,
            )
            server = build_server(ctx)
            if transport == TRANSPORT_HTTP:
                run_http(
                    server,
                    host=host,
                    port=port,
                    path=path,
                    log_level=log_level,
                    data_root=ctx.data_root,
                    server_instance_id=ctx.server_instance_id,
                    credentials=credentials,
                )
            else:
                run_stdio(server)
        except NarumiError as exc:
            click.echo(json.dumps(exc.to_payload(), ensure_ascii=False), err=True)
            sys.exit(ERROR_EXIT_CODE)
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
        except ShutdownRequested as exc:
            log.info("%s; shutting down", exc)
        finally:
            try:
                if ctx is not None:
                    ctx.close()
            finally:
                if credentials is not None:
                    credentials.close()
                if lease is not None:
                    lease.close()


def main() -> None:
    """Console-script entry point (``narumi-server``)."""
    cli(prog_name="narumi-server")


if __name__ == "__main__":  # pragma: no cover
    main()
