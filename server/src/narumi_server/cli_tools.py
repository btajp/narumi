"""Product CLI ``narumi``: a contract-driven 1:1 mapping of the MCP tools.

Subcommands are generated from ``contracts/`` at start-up — one per tool
(``list_meetings`` → ``narumi list-meetings``) plus generic JSON/stdin input.
Resident calls require an owner-validated bootstrap, pinned TLS and client authentication.
Only local, non-secret tools may run in-process when no bootstrap exists and no server URL
was selected. A security, protocol or contract error never falls back or resends a call.
Recording, provider and write-only tools always require the resident server.

Success prints the tool's structured content as JSON on stdout (``--pretty``
by default, ``--raw`` for one line); every failure prints the contract
``error_envelope`` on stderr and exits 2.
"""

from __future__ import annotations

import contextlib
import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, replace
from email.message import Message
from pathlib import Path
from typing import Any

import click
from narumi.config import ENV_HOME
from narumi.config import data_root as resolve_data_root
from narumi.contracts import ContractSet, ToolContract, load_contracts
from narumi.errors import ContractMismatchError, ErrorCode, InvalidArgumentError, NarumiError

from narumi_server import __version__
from narumi_server.app import dispatch
from narumi_server.cli_input import (
    build_tool_input,
    collect_args,
    contains_secret_value,
    parse_json_option,
    read_stdin,
    secret_strings,
    with_request_id,
)
from narumi_server.cli_transport import ConfidentialHttpTransport, confidential_endpoint
from narumi_server.context import build_context
from narumi_server.secure_transport import (
    BootstrapNotFoundError,
    ClientTransport,
    TransportSecurityError,
    load_client_transport,
)
from narumi_server.transport_logging import install_transport_log_filters

ENV_SERVER_URL = "NARUMI_SERVER_URL"
ERROR_EXIT_CODE = 2
TRANSPORT_CLI = "cli"
RECORDING_TOOLS = frozenset({"start_recording", "stop_recording", "get_recording_status"})
"""Tools that need the resident server: in-process the recorder dies with the CLI process."""
PROVIDER_TOOLS = frozenset(
    {
        "list_providers",
        "list_provider_connections",
        "set_provider_connection",
        "delete_provider_connection",
        "authenticate_provider_connection",
        "get_provider_auth_status",
        "test_provider_connection",
        "list_provider_models",
        "prepare_provider_runtime",
    }
)
RESIDENT_SERVER_TOOLS = RECORDING_TOOLS | PROVIDER_TOOLS | {"configure_recording_permission"}

MODE_AUTO = "auto"
MODE_IN_PROCESS = "in-process"
MODE_REQUIRE_SERVER = "require-server"

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION})
PROBE_TIMEOUT = 1.0
"""Seconds for each probe round-trip (connect + ``initialize``)."""
PROBE_CALL_TIMEOUT = 2.0
"""Seconds for the probe's ``get_server_info`` call (it may run ``narumi-recorder check``)."""
CALL_TIMEOUT = 600.0
"""Bound for the real tool call. Generous: tools enqueue jobs instead of awaiting them."""
MAX_RESPONSE_BYTES = 8_388_608

GENERIC_COMMAND = "tool"

_JSON_CONTENT_TYPE = "application/json"
_SSE_CONTENT_TYPE = "text/event-stream"
_SECRET_TOOL_META = "narumi_secret_tool"


@dataclass(frozen=True)
class CliState:
    """Global options plus the per-call transport policy derived from the contract."""

    server_url: str | None
    mode: str
    data_root: Path | None
    pretty: bool
    confidential: bool = False
    contract_version: str = "4.0.0"


def _redacted_error_payload(
    tool: str, code: ErrorCode = ErrorCode.INVALID_ARGUMENT
) -> dict[str, Any]:
    try:
        code = ErrorCode(code)
    except (TypeError, ValueError):
        code = ErrorCode.INTERNAL
    message = "Invalid command input" if code == ErrorCode.INVALID_ARGUMENT else "Tool call failed"
    if code == ErrorCode.CONTRACT_MISMATCH:
        message = "Contract major mismatch; update narumi.app, narumi-server and the CLI together"
    elif code == ErrorCode.AUTHENTICATION_REQUIRED:
        message = "An authenticated resident narumi-server is required; open narumi.app and retry"
    return {"error": {"code": str(code), "message": message, "details": {"tool": tool}}}


class _RedactedUsageError(click.ClickException):
    """A Click parser failure with no raw option names, values or exception text."""

    exit_code = ERROR_EXIT_CODE

    def __init__(self, tool: str, *, pretty: bool) -> None:
        super().__init__("Invalid command input")
        self.payload = _redacted_error_payload(tool)
        self.pretty = pretty

    def show(self, file: Any = None) -> None:
        click.echo(_render(self.payload, pretty=self.pretty), file=file, err=file is None)


def _positional_tokens(args: list[str], params: list[click.Parameter]) -> Iterator[tuple[int, str]]:
    """Find positional tokens without decoding or retaining any option values."""
    options = {
        flag: param
        for param in params
        if isinstance(param, click.Option)
        for flag in (*param.opts, *param.secondary_opts)
    }
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            yield from enumerate(args[index + 1 :], start=index + 1)
            return
        if token.startswith("-"):
            option = options.get(token.partition("=")[0])
            if option is not None and not option.is_flag and "=" not in token:
                index += option.nargs
        else:
            yield index, token
        index += 1


class _ContractGroup(click.Group):
    """Guard Click's pre-callback diagnostics for commands with write-only input."""

    def __init__(self, *args: Any, contracts: ContractSet, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.contracts = contracts

    def _selected_contract(self, args: list[str]) -> ToolContract | None:
        first = next(_positional_tokens(args, self.params), None)
        if first is None:
            return None
        index, command = first
        if command == GENERIC_COMMAND:
            generic = self.commands[GENERIC_COMMAND]
            first = next(_positional_tokens(args[index + 1 :], generic.params), None)
            if first is None:
                return None
            _, command = first
        return self.contracts.get(command.replace("-", "_"))

    @staticmethod
    def _redact_usage(ctx: click.Context) -> None:
        tool = ctx.meta.get(_SECRET_TOOL_META)
        if tool is not None:
            state = ctx.obj
            raise _RedactedUsageError(
                tool, pretty=state.pretty if isinstance(state, CliState) else True
            ) from None

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        contract = self._selected_contract(args)
        ctx.meta[_SECRET_TOOL_META] = (
            contract.name
            if contract is not None
            and (contract.has_write_only_input or contract.name in PROVIDER_TOOLS)
            else None
        )
        try:
            return super().parse_args(ctx, args)
        except click.ClickException:
            self._redact_usage(ctx)
            raise

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.ClickException:
            self._redact_usage(ctx)
            raise


# ---------------------------------------------------------------------------- HTTP client
class ServerUnreachableError(Exception):
    """The server URL did not answer as an MCP endpoint (connection, timeout or protocol)."""


def _sse_messages(text: str) -> list[Any]:
    """JSON payloads of the ``data:`` events in one SSE body (non-JSON events are skipped)."""
    messages: list[Any] = []
    data_lines: list[str] = []
    for raw_line in [*text.splitlines(), ""]:
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                with contextlib.suppress(ValueError):
                    messages.append(json.loads("\n".join(data_lines)))
                data_lines = []
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].removeprefix(" "))
    return messages


class McpHttpClient:
    """Minimal synchronous MCP Streamable HTTP client for one-shot CLI calls.

    Speaks just enough JSON-RPC: ``initialize`` → ``notifications/initialized`` →
    ``tools/call``, plus a best-effort DELETE to end the session. Accepts both
    ``application/json`` and ``text/event-stream`` responses and reuses the
    ``Mcp-Session-Id`` the server assigns.
    """

    def __init__(self, credentials: ClientTransport) -> None:
        self._transport = ConfidentialHttpTransport(credentials)
        self.url = self._transport.url
        self.server_instance_id = credentials.server_instance_id
        self.session_id: str | None = None
        self.negotiated_version: str | None = None
        self._next_id = 0

    # -------------------------------------------------------------- wire helpers
    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        return self._transport.open(request, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": _JSON_CONTENT_TYPE,
            "Accept": f"{_JSON_CONTENT_TYPE}, {_SSE_CONTENT_TYPE}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.negotiated_version:
            headers["MCP-Protocol-Version"] = self.negotiated_version
        return headers

    def _post(self, message: dict[str, Any], timeout: float) -> tuple[int, Message, bytes]:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(  # scheme checked in __init__
            self.url, data=data, headers=self._headers(), method="POST"
        )
        try:
            with self._open(request, timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ServerUnreachableError("Resident server response exceeds the size limit")
                return response.status, response.headers, body
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise NarumiError(
                    "Resident server authentication failed; restart narumi.app and retry",
                    code=ErrorCode.AUTHENTICATION_REQUIRED,
                ) from None
            raise ServerUnreachableError("Resident server rejected the HTTP request") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if isinstance(getattr(exc, "reason", exc), ssl.SSLError):
                raise TransportSecurityError() from None
            raise ServerUnreachableError(
                "Could not establish the authenticated server connection"
            ) from None

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        _status, headers, body = self._post(message, timeout)
        session = headers.get("mcp-session-id")
        if session:
            if not re.fullmatch(r"[!-~]{1,512}", session):
                raise ServerUnreachableError("Resident server returned an invalid session header")
            self.session_id = session
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        try:
            if content_type == _JSON_CONTENT_TYPE:
                candidates: list[Any] = [json.loads(body)]
            elif content_type == _SSE_CONTENT_TYPE:
                candidates = _sse_messages(body.decode("utf-8"))
            else:
                raise ServerUnreachableError("Resident server returned an unsupported response")
        except (ValueError, RecursionError, UnicodeError):
            raise ServerUnreachableError("Resident server returned invalid JSON") from None
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("id") in (request_id, str(request_id))
                and ("result" in candidate or "error" in candidate)
            ):
                return candidate
        raise ServerUnreachableError("Resident server returned no matching JSON-RPC response")

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method}, PROBE_TIMEOUT)

    # -------------------------------------------------------------- MCP surface
    def initialize(self, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
        response = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "narumi", "version": __version__},
            },
            timeout,
        )
        error = response.get("error")
        if error is not None:
            raise ServerUnreachableError("Resident server initialization failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ServerUnreachableError("Resident server initialization returned no result")
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ServerUnreachableError("Resident server returned an unsupported protocol version")
        self.negotiated_version = version
        self._notify("notifications/initialized")
        return result

    def probe(self, expected_contract_version: str = "4.0.0") -> None:
        """``initialize`` + ``get_server_info`` with short timeouts.

        No user operation is sent until the resident server proves contract compatibility.
        """
        self.initialize(PROBE_TIMEOUT)
        payload, is_error = self._call_tool("get_server_info", {}, PROBE_CALL_TIMEOUT)
        if is_error or payload.get("name") != "narumi":
            raise ServerUnreachableError("Resident server compatibility check failed")
        expected, actual = (
            _contract_major(expected_contract_version),
            _contract_major(payload.get("contract_version")),
        )
        if expected is None or actual != expected:
            raise ContractMismatchError(
                "Contract major mismatch; update narumi.app, narumi-server and the CLI together",
                details={"expected_major": expected, "server_major": actual},
            )
        if payload.get("server_instance_id") != self.server_instance_id:
            raise ServerUnreachableError("Resident server instance no longer matches the bootstrap")

    def call_tool(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return self._call_tool(tool, args, CALL_TIMEOUT)

    def _call_tool(
        self, tool: str, args: dict[str, Any], timeout: float
    ) -> tuple[dict[str, Any], bool]:
        response = self._request("tools/call", {"name": tool, "arguments": args}, timeout)
        error = response.get("error")
        if error is not None:
            raise NarumiError(
                "Resident server rejected the tool call",
                code=ErrorCode.INTERNAL,
                details={"tool": tool},
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise ServerUnreachableError("Resident server returned no tool result")
        payload = result.get("structuredContent")
        if payload is None:
            payload = _payload_from_content(result)
        if not isinstance(payload, dict):
            raise NarumiError(
                f"tool {tool} returned no structured content",
                code=ErrorCode.INTERNAL,
                details={"tool": tool},
            )
        return payload, bool(result.get("isError", False))

    def close(self) -> None:
        """Best-effort ``DELETE`` ending the session; never raises."""
        if not self.session_id:
            return
        request = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
        with contextlib.suppress(Exception):
            with self._open(request, PROBE_TIMEOUT):
                pass


def _contract_major(version: Any) -> int | None:
    if (
        not isinstance(version, str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.+-]+)?",
            version,
        )
        is None
    ):
        return None
    return int(version.split(".", 1)[0])


def _payload_from_content(result: dict[str, Any]) -> Any:
    """Fallback for servers that answer with text content only: parse the first text block."""
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                return json.loads(item.get("text", ""))
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------- execution
def _call(state: CliState, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Run one tool call: server first (unless ``--in-process``), else in this process."""
    if state.mode != MODE_IN_PROCESS:
        over_http = _call_over_http(state, tool, args)
        if over_http is not None:
            return over_http
    return _call_in_process(state, tool, args)


def _call_over_http(
    state: CliState, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], bool] | None:
    """Only a missing bootstrap can permit local execution; failures never cause replay."""
    if state.server_url is not None:
        confidential_endpoint(state.server_url)
    install_transport_log_filters()
    try:
        credentials = load_client_transport(
            resolve_data_root(state.data_root), expected_url=state.server_url
        )
    except BootstrapNotFoundError:
        if (
            state.mode == MODE_AUTO
            and state.server_url is None
            and tool not in RESIDENT_SERVER_TOOLS
            and not state.confidential
        ):
            return None
        raise
    client = McpHttpClient(credentials)
    try:
        try:
            client.probe(state.contract_version)
            return client.call_tool(tool, args)
        except ServerUnreachableError:
            raise NarumiError(
                "Authenticated narumi-server call failed; the operation was not retried. "
                "Check the server and query operation status before trying again.",
                code=ErrorCode.INTERNAL,
                details={"tool": tool},
            ) from None
    finally:
        client.close()


def _call_in_process(
    state: CliState, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    if state.confidential or tool in PROVIDER_TOOLS:
        raise NarumiError(
            "Provider and write-only tools require an authenticated resident narumi-server",
            code=ErrorCode.AUTHENTICATION_REQUIRED,
            details={"tool": tool},
        )
    if tool in RESIDENT_SERVER_TOOLS:
        raise InvalidArgumentError(
            f"{tool} needs a resident narumi-server to keep recorder ownership and operation "
            "locking in one process. Start narumi.app or `narumi-server --http` and retry.",
            details={"tool": tool},
        )
    ctx = build_context(state.data_root, transports=[TRANSPORT_CLI])
    try:
        outcome = dispatch(ctx, tool, args)
        return outcome.payload, outcome.is_error
    finally:
        ctx.close()


def _render(payload: dict[str, Any], *, pretty: bool) -> str:
    if pretty:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _echoes_secret(value: Any, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(
            _echoes_secret(key, secrets) or _echoes_secret(item, secrets)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_echoes_secret(item, secrets) for item in value)
    return False


def _run(
    state: CliState,
    tool: str,
    args: dict[str, Any],
    *,
    redact: bool = False,
    secrets: tuple[str, ...] = (),
) -> None:
    """Execute and print: result JSON on stdout, error envelope on stderr with exit 2."""
    try:
        # Use the same contract decision for input privacy and the entire HTTP session.
        payload, is_error = _call(replace(state, confidential=redact), tool, args)
        if secrets and _echoes_secret(payload, secrets):
            payload, is_error = _redacted_error_payload(tool, ErrorCode.INTERNAL), True
    except NarumiError as exc:
        payload = _redacted_error_payload(tool, exc.code) if redact else exc.to_payload()
        is_error = True
    except Exception:
        if not redact:
            raise
        payload, is_error = _redacted_error_payload(tool, ErrorCode.INTERNAL), True
    if is_error:
        if redact:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else ErrorCode.INTERNAL
            payload = _redacted_error_payload(tool, code)
        click.echo(_render(payload, pretty=state.pretty), err=True)
        sys.exit(ERROR_EXIT_CODE)
    click.echo(_render(payload, pretty=state.pretty))


# ---------------------------------------------------------------------------- CLI assembly
def _make_tool_command(contract: ToolContract) -> click.Command:
    inputs = build_tool_input(contract)
    private = contract.has_write_only_input or contract.name in PROVIDER_TOOLS

    def callback(**kwargs: Any) -> None:
        state: CliState = click.get_current_context().obj
        try:
            args = collect_args(contract, inputs, kwargs)
        except NarumiError as exc:
            payload = (
                _redacted_error_payload(contract.name, exc.code) if private else exc.to_payload()
            )
            click.echo(_render(payload, pretty=state.pretty), err=True)
            sys.exit(ERROR_EXIT_CODE)
        _run(state, contract.name, args, redact=private, secrets=secret_strings(contract, args))

    return click.Command(
        name=contract.name.replace("_", "-"),
        params=list(inputs.options),
        callback=callback,
        help=contract.description,
        short_help=contract.title,
    )


def _make_generic_command(contracts: ContractSet) -> click.Command:
    def callback(name: str, args_json: str | None, json_stdin: bool) -> None:
        state: CliState = click.get_current_context().obj
        tool = name.replace("-", "_")
        contract = contracts.get(tool)
        private = contract is not None and (contract.has_write_only_input or tool in PROVIDER_TOOLS)
        try:
            if contract is None:
                raise InvalidArgumentError(
                    f"unknown tool: {name}",
                    details={"tool": name, "known_tools": contracts.tool_names()},
                )
            if args_json is not None and json_stdin:
                raise InvalidArgumentError("--json and --json-stdin are mutually exclusive")
            document = (
                read_stdin() if json_stdin else (args_json if args_json is not None else "{}")
            )
            args = parse_json_option("json", document, redact=private or json_stdin)
            if not isinstance(args, dict):
                raise InvalidArgumentError(
                    "--json must be a JSON object of tool arguments",
                    details={
                        "tool": tool,
                        **({} if private or json_stdin else {"value": args_json}),
                    },
                )
            if not json_stdin and contains_secret_value(contract, args):
                raise InvalidArgumentError(
                    "--json cannot contain write-only values; use --json-stdin or a hidden prompt"
                )
        except NarumiError as exc:
            payload = _redacted_error_payload(tool, exc.code) if private else exc.to_payload()
            click.echo(_render(payload, pretty=state.pretty), err=True)
            sys.exit(ERROR_EXIT_CODE)
        _run(
            state,
            tool,
            with_request_id(contract, args),
            redact=private,
            secrets=secret_strings(contract, args),
        )

    return click.Command(
        name=GENERIC_COMMAND,
        params=[
            click.Argument(["name"]),
            click.Option(
                ["--json", "args_json"],
                default=None,
                help="Non-secret JSON arguments (default: {}; request_id is added when omitted).",
            ),
            click.Option(
                ["--json-stdin"],
                is_flag=True,
                help="Read the JSON object from stdin; required for write-only values.",
            ),
        ],
        callback=callback,
        help=(
            "Call any contract tool by name with raw JSON arguments, e.g. "
            "`narumi tool list_meetings --json '{\"limit\": 5}'`."
        ),
        short_help="Generic tool call (escape hatch)",
    )


def build_cli(contracts: ContractSet | None = None) -> click.Group:
    """Build the ``narumi`` group: one subcommand per contract tool plus ``tool``."""
    contract_set = contracts if contracts is not None else load_contracts()

    @click.group(
        cls=_ContractGroup,
        contracts=contract_set,
        context_settings={"help_option_names": ["-h", "--help"]},
    )
    @click.version_option(__version__, prog_name="narumi")
    @click.option(
        "--server-url",
        envvar=ENV_SERVER_URL,
        show_envvar=True,
        default=None,
        help="Authenticated HTTPS endpoint; must match this data root's trusted bootstrap.",
    )
    @click.option(
        "--in-process",
        "mode",
        flag_value=MODE_IN_PROCESS,
        help="Run local tools in this process; provider, recording and write-only tools refuse.",
    )
    @click.option(
        "--require-server",
        "mode",
        flag_value=MODE_REQUIRE_SERVER,
        help="Fail instead of falling back in-process when the server is unreachable.",
    )
    @click.option(
        "--data-root",
        type=click.Path(file_okay=False, path_type=Path),
        envvar=ENV_HOME,
        show_envvar=True,
        default=None,
        help="Data root used for trusted server discovery and local execution.",
    )
    @click.option(
        "--pretty/--raw",
        "pretty",
        default=True,
        help="Pretty-print the JSON output (--raw prints one line).",
    )
    @click.pass_context
    def cli(
        ctx: click.Context,
        server_url: str | None,
        mode: str | None,
        data_root: Path | None,
        pretty: bool,
    ) -> None:
        """narumi product CLI: the MCP tools from contracts/, one subcommand each.

        Resident calls use pinned TLS and client authentication from the trusted bootstrap.
        Without a bootstrap, local tools may run in-process unless a server URL was specified.
        Errors use the contract error envelope on stderr with exit code 2.
        """
        ctx.obj = CliState(
            server_url=server_url,
            mode=mode or MODE_AUTO,
            data_root=data_root,
            pretty=pretty,
            contract_version=contract_set.contract_version,
        )

    for contract in contract_set:
        cli.add_command(_make_tool_command(contract))
    cli.add_command(_make_generic_command(contract_set))
    return cli


def main() -> None:
    """Console-script entry point (``narumi``)."""
    try:
        cli = build_cli()
    except NarumiError as exc:  # contracts unreadable / inconsistent
        click.echo(json.dumps(exc.to_payload(), ensure_ascii=False), err=True)
        sys.exit(ERROR_EXIT_CODE)
    cli(prog_name="narumi")


if __name__ == "__main__":  # pragma: no cover
    main()
