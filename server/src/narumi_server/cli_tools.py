"""Product CLI ``narumi``: a contract-driven 1:1 mapping of the MCP tools.

Subcommands are generated from ``contracts/`` at start-up — one per tool
(``list_meetings`` → ``narumi list-meetings``) plus the generic escape hatch
``narumi tool <name> --json '{...}'``. The CLI never calls the library directly
(that is ``narumi-dev``): a call goes to a running ``narumi-server`` when the
``--server-url`` endpoint answers an MCP probe (``initialize`` +
``get_server_info`` with ~1s timeouts), otherwise it runs in this process
through the same ``narumi_server.app.dispatch`` code path the server uses.
``--require-server`` / ``--in-process`` force either side. Recording tools
(``start_recording`` / ``stop_recording`` / ``get_recording_status``) are
refused in-process: the recording would die with the CLI process.

Success prints the tool's structured content as JSON on stdout (``--pretty``
by default, ``--raw`` for one line); every failure prints the contract
``error_envelope`` on stderr and exits 2.
"""

from __future__ import annotations

import contextlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, replace
from email.message import Message
from pathlib import Path
from typing import Any

import click
from narumi.config import DEFAULT_HTTP_PORT, ENV_HOME
from narumi.contracts import ContractSet, ToolContract, load_contracts
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError

from narumi_server import __version__
from narumi_server.app import dispatch
from narumi_server.cli_input import (
    build_tool_input,
    collect_args,
    parse_json_option,
    with_request_id,
)
from narumi_server.cli_transport import ConfidentialHttpTransport
from narumi_server.context import build_context

ENV_SERVER_URL = "NARUMI_SERVER_URL"
DEFAULT_SERVER_URL = f"http://127.0.0.1:{DEFAULT_HTTP_PORT}/mcp"
ERROR_EXIT_CODE = 2
TRANSPORT_CLI = "cli"
RECORDING_TOOLS = frozenset({"start_recording", "stop_recording", "get_recording_status"})
"""Tools that need the resident server: in-process the recorder dies with the CLI process."""

MODE_AUTO = "auto"
MODE_IN_PROCESS = "in-process"
MODE_REQUIRE_SERVER = "require-server"

PROTOCOL_VERSION = "2025-06-18"
PROBE_TIMEOUT = 1.0
"""Seconds for each probe round-trip (connect + ``initialize``)."""
PROBE_CALL_TIMEOUT = 2.0
"""Seconds for the probe's ``get_server_info`` call (it may run ``narumi-recorder check``)."""
CALL_TIMEOUT = 600.0
"""Bound for the real tool call. Generous: tools enqueue jobs instead of awaiting them."""

GENERIC_COMMAND = "tool"

_JSON_CONTENT_TYPE = "application/json"
_SSE_CONTENT_TYPE = "text/event-stream"
_SECRET_TOOL_META = "narumi_secret_tool"


@dataclass(frozen=True)
class CliState:
    """Global options plus the per-call transport policy derived from the contract."""

    server_url: str
    mode: str
    data_root: Path | None
    pretty: bool
    confidential: bool = False


def _redacted_error_payload(
    tool: str, code: ErrorCode = ErrorCode.INVALID_ARGUMENT
) -> dict[str, Any]:
    try:
        code = ErrorCode(code)
    except (TypeError, ValueError):
        code = ErrorCode.INTERNAL
    message = "Invalid command input" if code == ErrorCode.INVALID_ARGUMENT else "Tool call failed"
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
            contract.name if contract is not None and contract.has_write_only_input else None
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


def _snippet(body: bytes, limit: int = 200) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    return text[:limit]


class McpHttpClient:
    """Minimal synchronous MCP Streamable HTTP client for one-shot CLI calls.

    Speaks just enough JSON-RPC: ``initialize`` → ``notifications/initialized`` →
    ``tools/call``, plus a best-effort DELETE to end the session. Accepts both
    ``application/json`` and ``text/event-stream`` responses and reuses the
    ``Mcp-Session-Id`` the server assigns.
    """

    def __init__(self, url: str, *, confidential: bool = False) -> None:
        self._transport = ConfidentialHttpTransport(url) if confidential else None
        if self._transport is not None:
            url = self._transport.url
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise InvalidArgumentError(
                f"--server-url must be an http(s) URL: {url!r}", details={"server_url": url}
            )
        self.url = url
        self.session_id: str | None = None
        self.negotiated_version: str | None = None
        self._next_id = 0

    # -------------------------------------------------------------- wire helpers
    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        if self._transport is not None:
            return self._transport.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout)

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
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            snippet = _snippet(exc.read())
            raise ServerUnreachableError(f"HTTP {exc.code} from {self.url}: {snippet}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            raise ServerUnreachableError(str(reason if reason is not None else exc)) from exc

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        status, headers, body = self._post(message, timeout)
        session = headers.get("mcp-session-id")
        if session:
            self.session_id = session
        content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
        try:
            if content_type == _JSON_CONTENT_TYPE:
                candidates: list[Any] = [json.loads(body)]
            elif content_type == _SSE_CONTENT_TYPE:
                candidates = _sse_messages(body.decode("utf-8"))
            else:
                raise ServerUnreachableError(
                    f"unexpected response from {self.url} "
                    f"(status {status}, content type {content_type or 'missing'})"
                )
        except ValueError as exc:
            raise ServerUnreachableError(f"invalid JSON from {self.url}: {exc}") from exc
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("id") in (request_id, str(request_id))
                and ("result" in candidate or "error" in candidate)
            ):
                return candidate
        raise ServerUnreachableError(f"no JSON-RPC response to {method!r} from {self.url}")

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
            raise ServerUnreachableError(f"initialize failed: {error}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ServerUnreachableError(f"initialize returned no result from {self.url}")
        version = result.get("protocolVersion")
        if isinstance(version, str):
            self.negotiated_version = version
        self._notify("notifications/initialized")
        return result

    def probe(self) -> None:
        """``initialize`` + ``get_server_info`` with short timeouts.

        Raises :class:`ServerUnreachableError` when nothing MCP-shaped answers. A reachable
        server whose ``get_server_info`` returns an error envelope still counts as reachable.
        """
        self.initialize(PROBE_TIMEOUT)
        self._call_tool("get_server_info", {}, PROBE_CALL_TIMEOUT)

    def call_tool(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return self._call_tool(tool, args, CALL_TIMEOUT)

    def _call_tool(
        self, tool: str, args: dict[str, Any], timeout: float
    ) -> tuple[dict[str, Any], bool]:
        response = self._request("tools/call", {"name": tool, "arguments": args}, timeout)
        error = response.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            raise NarumiError(
                f"server rejected the call: {message or error}",
                code=ErrorCode.INTERNAL,
                details={"tool": tool, "jsonrpc_error": error},
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise ServerUnreachableError(f"tools/call returned no result from {self.url}")
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
            with self._open(request, PROBE_TIMEOUT) as response:
                response.read()


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
    """``None`` when no server answers the probe and falling back in-process is allowed."""
    client = McpHttpClient(state.server_url, confidential=state.confidential)
    try:
        try:
            client.probe()
        except ServerUnreachableError as exc:
            if state.mode == MODE_REQUIRE_SERVER:
                raise NarumiError(
                    f"narumi-server at {state.server_url} is not reachable "
                    f"(--require-server): {exc}",
                    code=ErrorCode.INTERNAL,
                    details={"server_url": state.server_url},
                ) from exc
            return None
        try:
            return client.call_tool(tool, args)
        except ServerUnreachableError as exc:
            # The probe succeeded, so the call may have reached the server: surface the
            # failure instead of retrying in-process (the tool could run twice).
            raise NarumiError(
                f"lost the connection to narumi-server at {state.server_url}: {exc}",
                code=ErrorCode.INTERNAL,
                details={"server_url": state.server_url, "tool": tool},
            ) from exc
    finally:
        client.close()


def _call_in_process(
    state: CliState, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    if tool in RECORDING_TOOLS:
        raise InvalidArgumentError(
            f"{tool} needs a resident narumi-server (a recording would stop with this CLI "
            "process). Start `narumi-server --http` or narumi.app and retry, or point "
            "--server-url / NARUMI_SERVER_URL at a running server.",
            details={"tool": tool, "server_url": state.server_url},
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


def _run(state: CliState, tool: str, args: dict[str, Any], *, redact: bool = False) -> None:
    """Execute and print: result JSON on stdout, error envelope on stderr with exit 2."""
    try:
        # Use the same contract decision for input privacy and the entire HTTP session.
        payload, is_error = _call(replace(state, confidential=redact), tool, args)
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

    def callback(**kwargs: Any) -> None:
        state: CliState = click.get_current_context().obj
        try:
            args = collect_args(contract, inputs, kwargs)
        except NarumiError as exc:
            payload = (
                _redacted_error_payload(contract.name, exc.code)
                if contract.has_write_only_input
                else exc.to_payload()
            )
            click.echo(_render(payload, pretty=state.pretty), err=True)
            sys.exit(ERROR_EXIT_CODE)
        _run(state, contract.name, args, redact=contract.has_write_only_input)

    return click.Command(
        name=contract.name.replace("_", "-"),
        params=list(inputs.options),
        callback=callback,
        help=contract.description,
        short_help=contract.title,
    )


def _make_generic_command(contracts: ContractSet) -> click.Command:
    def callback(name: str, args_json: str) -> None:
        state: CliState = click.get_current_context().obj
        tool = name.replace("-", "_")
        contract = contracts.get(tool)
        try:
            if contract is None:
                raise InvalidArgumentError(
                    f"unknown tool: {name}",
                    details={"tool": name, "known_tools": contracts.tool_names()},
                )
            args = parse_json_option("json", args_json, redact=contract.has_write_only_input)
            if not isinstance(args, dict):
                raise InvalidArgumentError(
                    "--json must be a JSON object of tool arguments",
                    details={
                        "tool": tool,
                        **({} if contract.has_write_only_input else {"value": args_json}),
                    },
                )
        except NarumiError as exc:
            payload = (
                _redacted_error_payload(tool, exc.code)
                if contract is not None and contract.has_write_only_input
                else exc.to_payload()
            )
            click.echo(_render(payload, pretty=state.pretty), err=True)
            sys.exit(ERROR_EXIT_CODE)
        _run(
            state,
            tool,
            with_request_id(contract, args),
            redact=contract.has_write_only_input,
        )

    return click.Command(
        name=GENERIC_COMMAND,
        params=[
            click.Argument(["name"]),
            click.Option(
                ["--json", "args_json"],
                default="{}",
                show_default=True,
                help="Tool arguments as one JSON object (request_id is added when omitted).",
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
        default=DEFAULT_SERVER_URL,
        show_default=True,
        help="narumi-server Streamable HTTP endpoint tried first.",
    )
    @click.option(
        "--in-process",
        "mode",
        flag_value=MODE_IN_PROCESS,
        help="Never contact a server; run the tool in this process (recording tools refuse).",
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
        help="Data root for in-process execution (a server uses its own data root).",
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
        server_url: str,
        mode: str | None,
        data_root: Path | None,
        pretty: bool,
    ) -> None:
        """narumi product CLI: the MCP tools from contracts/, one subcommand each.

        With a running narumi-server the call is sent over Streamable HTTP; otherwise it
        runs in-process against the same data root. Output is the tool's JSON result;
        errors are the contract error envelope on stderr with exit code 2.
        """
        ctx.obj = CliState(
            server_url=server_url,
            mode=mode or MODE_AUTO,
            data_root=data_root,
            pretty=pretty,
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
